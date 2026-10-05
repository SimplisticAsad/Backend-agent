# ac.task.update_status  (requirements: requirement.task_status; operations: operation.task.update_status)
Feature: Update task status succeeds

  Scenario: Update task status succeeds
    Given the user is signed in as Manager or Employee
    And the record being worked on exists
    When Choose the new status
    And Update task status
    Then The task shows its new status
