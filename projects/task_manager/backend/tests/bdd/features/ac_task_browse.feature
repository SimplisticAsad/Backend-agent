# ac.task.browse  (requirements: requirement.task_management; operations: operation.task.list, operation.task.read)
Feature: Browse Tasks succeeds

  Scenario: Browse Tasks succeeds
    Given the user is signed in as Manager or Employee
    And the record being worked on exists
    When List Tasks
    And View Task
    Then The Task information is displayed
