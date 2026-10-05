# ac.department.update  (requirements: requirement.department_management; operations: operation.department.update)
Feature: Update Department succeeds

  Scenario: Update Department succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Change the Department details
    And Update Department
    Then The changes to the Department are saved
